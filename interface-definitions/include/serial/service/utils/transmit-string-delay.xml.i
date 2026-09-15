<!-- include start from serial/service/utils/transmit-string-delay.xml.i -->
<node name="transmit-string">
  <properties>
    <help>Transmit string settings</help>
  </properties>
  <children>
    <leafNode name="delay-after-transmit">
      <properties>
        <help>Delay after transmitting string</help>
        <valueHelp>
          <format>u32:0-65535</format>
          <description>Specifies the delay in milliseconds</description>
        </valueHelp>
        <constraint>
          <validator name="numeric" argument="--range 0-65535"/>
        </constraint>
      </properties>
      <defaultValue>10</defaultValue>
    </leafNode>
  </children>
</node>
<!-- include end -->
