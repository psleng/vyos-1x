<!-- include start from serial/service/utils/initiate.xml.i -->
<node name="initiate">
  <properties>
    <help>Initiate connection settings</help>
  </properties>
  <children>
    <leafNode name="any-character">
      <properties>
        <help>Connect when any data is received</help>
        <valueless/>
      </properties>
    </leafNode>
    <leafNode name="character">
      <properties>
        <help>Connect when specific character received</help>
        <valueHelp>
          <format>txt</format>
          <description>ASCII char in hex value</description>
        </valueHelp>
        <constraint>
          <validator name="hex"/>
        </constraint>
      </properties>
    </leafNode>
  </children>
</node>
<!-- include end -->
