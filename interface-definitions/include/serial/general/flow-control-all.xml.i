<!-- include start from serial/general/flow-control-all.xml.i -->
<node name="flow-control">
  <properties>
    <help>Flow control</help>
  </properties>
  <children>
    <leafNode name="none">
      <properties>
        <help>No flow control (default)</help>
        <valueless/>
      </properties>
    </leafNode>
    <node name="both">
      <properties>
        <help>Turn on hardware and software flow control</help>
      </properties>
      <children>
        #include <include/serial/general/flow-control-direction.xml.i>
      </children>
    </node>
    <node name="hardware">
      <properties>
        <help>Turn on hardware flow control only</help>
      </properties>
      <children>
        #include <include/serial/general/flow-control-direction.xml.i>
      </children>
    </node>
    <node name="software">
      <properties>
        <help>Turn on software flow control only</help>
      </properties>
      <children>
        #include <include/serial/general/flow-control-direction.xml.i>
      </children>
    </node>
  </children>
</node>
<!-- include end -->
