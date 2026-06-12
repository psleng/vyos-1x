<!-- include start from serial/service/nine-bits.xml.i -->
<node name="nine-bits">
  <properties>
    <help>Nine bits service settings</help>
  </properties>
  <children>
    #include <include/serial/service/utils/remote.xml.i>
    <leafNode name="delay">
      <properties>
        <help>The delay between writing first byte and rest of message to the serial port</help>
        <valueHelp>
          <format>u32:0-65535</format>
          <description>Specifies the delay in milliseconds</description>
        </valueHelp>
        <constraint>
          <validator name="numeric" argument="--range 0-65535"/>
        </constraint>
      </properties>
      <defaultValue>0</defaultValue>
    </leafNode>
  </children>
</node>
<!-- include end -->
